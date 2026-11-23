"""Integration tests for `cubrid-worktree delete`, including submodule and safety guards."""

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
class DeleteWorktreeTest(unittest.TestCase):
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
        (self.seed / ".gitignore").write_text("build/\n")
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "initial")
        self._git(self.seed, "push", "-u", "origin", "develop")
        self._git(self.root, "clone", self.remote, self.control)

    def _git(self, cwd: Path, *args: object, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [GIT, "-C", os.fspath(cwd), *map(str, args)],
            env=self.env,
            check=check,
            capture_output=True,
            text=True,
        )

    def _run_cli(self, *argv: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
        merged_env = dict(self.env)
        if extra_env:
            merged_env.update(extra_env)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(argv), env=merged_env)
        return code, stdout.getvalue(), stderr.getvalue()

    def _create_submodule_remote(self, name: str, *, add_to_seed_at: str | None = None) -> Path:
        bare = self.root / f"{name}.git"
        seed = self.root / f"{name}-seed"
        self._git(self.root, "init", "--bare", "--initial-branch=main", bare)
        self._git(self.root, "clone", bare, seed)
        (seed / "lib.txt").write_text(f"{name}\n")
        self._git(seed, "add", ".")
        self._git(seed, "commit", "-m", f"init {name}")
        self._git(seed, "push", "-u", "origin", "main")
        if add_to_seed_at is not None:
            self._git(self.seed, "-c", "protocol.file.allow=always", "submodule", "add", os.fspath(bare), add_to_seed_at)
            self._git(self.seed, "commit", "-m", f"add submodule {add_to_seed_at}")
            self._git(self.seed, "push", "origin", "develop")
        return bare

    def test_delete_clean_plain_worktree_keeps_branch_by_default(self) -> None:
        code, _, err = self._run_cli("create", "CBRD-28099-bench", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-28099-bench"
        build_dir = wt / "build"
        build_dir.mkdir()
        (build_dir / "artifact.o").write_text("ignored build output\n")

        del_code, del_out, del_err = self._run_cli("delete", "CBRD-28099-bench", "--json")
        self.assertEqual(del_code, 0, del_err)
        payload = json.loads(del_out)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertTrue(data["deleted"])
        self.assertFalse(data["dry_run"])
        self.assertEqual(data["name"], "CBRD-28099-bench")
        self.assertEqual(data["branch"], "CBRD-28099-bench")
        self.assertFalse(data["branch_deletion"]["deleted"])
        self.assertEqual(data["branch_deletion"]["status"], "skipped")
        self.assertFalse(wt.exists())

        branch_check = self._git(self.control, "show-ref", "--verify", "--quiet", "refs/heads/CBRD-28099-bench", check=False)
        self.assertEqual(branch_check.returncode, 0)

        _, list_out, _ = self._run_cli("list", "--json")
        listed_names = [row["name"] for row in json.loads(list_out)["data"]["worktrees"]]
        self.assertNotIn("CBRD-28099-bench", listed_names)

    def test_delete_with_delete_branch_deletes_merged_and_refuses_unmerged_without_force(self) -> None:
        code, _, err = self._run_cli("create", "CBRD-100", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        del_code, del_out, del_err = self._run_cli("delete", "CBRD-100", "--delete-branch", "--json")
        self.assertEqual(del_code, 0, del_err)
        self.assertTrue(json.loads(del_out)["data"]["branch_deletion"]["deleted"])
        self.assertEqual(
            self._git(self.control, "show-ref", "--verify", "--quiet", "refs/heads/CBRD-100", check=False).returncode,
            1,
        )

        # Unmerged branch: worktree is removed, branch -d refuses, branch remains until --force.
        code_unmerged, _, err_unmerged = self._run_cli("create", "CBRD-101", "--no-prepare", "--json")
        self.assertEqual(code_unmerged, 0, err_unmerged)
        wt_unmerged = self.parent / "CBRD-101"
        (wt_unmerged / "notes.txt").write_text("unmerged work\n")
        self._git(wt_unmerged, "add", "notes.txt")
        self._git(wt_unmerged, "commit", "-m", "unmerged commit")

        bfail_code, bfail_out, bfail_err = self._run_cli("delete", "CBRD-101", "--delete-branch", "--json")
        self.assertEqual(bfail_code, 1)
        self.assertEqual(bfail_out, "")
        err_payload = json.loads(bfail_err)
        self.assertEqual(err_payload["error"]["code"], "BRANCH_DELETE_FAILED")
        self.assertTrue(err_payload["error"]["details"]["worktree_deleted"])
        self.assertFalse(wt_unmerged.exists())
        self.assertEqual(
            self._git(self.control, "show-ref", "--verify", "--quiet", "refs/heads/CBRD-101", check=False).returncode,
            0,
        )

        # Unmerged branch with --delete-branch --force deletes both worktree and branch.
        code_force, _, err_force = self._run_cli("create", "CBRD-102", "--no-prepare", "--json")
        self.assertEqual(code_force, 0, err_force)
        wt_force = self.parent / "CBRD-102"
        (wt_force / "notes.txt").write_text("unmerged force\n")
        self._git(wt_force, "add", "notes.txt")
        self._git(wt_force, "commit", "-m", "unmerged force commit")

        ok_force, out_force, err_ok = self._run_cli("delete", "CBRD-102", "--delete-branch", "--force", "--json")
        self.assertEqual(ok_force, 0, err_ok)
        self.assertTrue(json.loads(out_force)["data"]["branch_deletion"]["deleted"])
        self.assertFalse(wt_force.exists())
        self.assertEqual(
            self._git(self.control, "show-ref", "--verify", "--quiet", "refs/heads/CBRD-102", check=False).returncode,
            1,
        )

    def test_delete_clean_worktree_with_initialized_submodule_without_force(self) -> None:
        self._create_submodule_remote("cubrid-cci", add_to_seed_at="cubrid-cci")

        code, _, err = self._run_cli("create", "CBRD-200", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-200"
        self._git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "cubrid-cci")

        common_gitdir = Path(self._git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
        wt_meta_dir = common_gitdir / "worktrees" / "CBRD-200"
        modules_dir = wt_meta_dir / "modules" / "cubrid-cci"
        self.assertTrue(modules_dir.is_dir())

        del_code, del_out, del_err = self._run_cli("delete", "CBRD-200", "--json")
        self.assertEqual(del_code, 0, del_err)
        data = json.loads(del_out)["data"]
        self.assertTrue(data["deleted"])
        self.assertEqual(data["submodules"]["discovered"], ["cubrid-cci"])
        self.assertEqual(data["submodules"]["populated"], ["cubrid-cci"])
        self.assertTrue(data["submodules"]["deinitialized"])
        self.assertTrue(data["submodules"]["modules_gitdir_present"])
        self.assertTrue(data["submodules"]["modules_gitdir_removed"])
        self.assertFalse(wt.exists())
        self.assertFalse(wt_meta_dir.exists())

    def test_delete_clean_worktree_with_unregistered_populated_submodule_without_force(self) -> None:
        self._create_submodule_remote("cubrid-jdbc", add_to_seed_at="cubrid-jdbc")
        vendor_bare = self._create_submodule_remote("vendor-sub")
        vendor_head = self._git(vendor_bare, "rev-parse", "main").stdout.strip()
        self._git(self.seed, "update-index", "--add", "--cacheinfo", f"160000,{vendor_head},vendor-sub")
        self._git(self.seed, "commit", "-m", "add gitlink vendor-sub without .gitmodules")
        self._git(self.seed, "push", "origin", "develop")

        code, _, err = self._run_cli("create", "CBRD-201", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-201"
        self._git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "cubrid-jdbc")

        # Mimic worktrees where submodule.cubrid-jdbc.url is unset AND a 160000 gitlink is populated
        # directly (e.g. without a .gitmodules mapping), so `git submodule deinit --all` skips vendor-sub.
        self._git(wt, "config", "--unset-all", "submodule.cubrid-jdbc.url", check=False)
        self._git(wt, "config", "--unset-all", "submodule.cubrid-jdbc.active", check=False)
        self._git(wt, "config", "--worktree", "--unset-all", "submodule.cubrid-jdbc.url", check=False)
        self._git(wt, "config", "--worktree", "--unset-all", "submodule.cubrid-jdbc.active", check=False)
        self._git(wt, "clone", vendor_bare, wt / "vendor-sub")
        self.assertNotEqual(
            self._git(wt, "config", "--get", "submodule.cubrid-jdbc.url", check=False).returncode,
            0,
        )
        self.assertTrue((wt / "cubrid-jdbc" / ".git").exists())
        self.assertTrue((wt / "vendor-sub" / ".git").exists())

        del_code, del_out, del_err = self._run_cli("delete", "CBRD-201", "--json")
        self.assertEqual(del_code, 0, del_err)
        data = json.loads(del_out)["data"]
        self.assertTrue(data["deleted"])
        self.assertEqual(sorted(data["submodules"]["populated"]), ["cubrid-jdbc", "vendor-sub"])
        self.assertIn("vendor-sub", data["submodules"]["cleaned_paths"])
        self.assertTrue(data["submodules"]["modules_gitdir_removed"])
        self.assertFalse(wt.exists())

    def test_delete_refuses_dirty_submodule_even_when_unregistered_and_succeeds_with_force(self) -> None:
        self._create_submodule_remote("cubrid-cci", add_to_seed_at="cubrid-cci")

        code, _, err = self._run_cli("create", "CBRD-202", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-202"
        self._git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "cubrid-cci")

        dirty_file = wt / "cubrid-cci" / "untracked.txt"
        dirty_file.write_text("unsaved submodule edit\n")

        # Registered dirty submodule is refused with DIRTY_SUBMODULE.
        fail1, _, err1 = self._run_cli("delete", "CBRD-202", "--json")
        self.assertEqual(fail1, 1)
        payload1 = json.loads(err1)
        self.assertEqual(payload1["error"]["code"], "DIRTY_SUBMODULE")
        self.assertIn("cubrid-cci", payload1["error"]["message"])
        self.assertTrue(dirty_file.is_file())

        # Unregistered dirty submodule is STILL caught and refused.
        self._git(wt, "config", "--unset-all", "submodule.cubrid-cci.url", check=False)
        self._git(wt, "config", "--worktree", "--unset-all", "submodule.cubrid-cci.url", check=False)
        fail2, _, err2 = self._run_cli("delete", "CBRD-202", "--json")
        self.assertEqual(fail2, 1)
        payload2 = json.loads(err2)
        self.assertEqual(payload2["error"]["code"], "DIRTY_SUBMODULE")
        self.assertTrue(dirty_file.is_file())

        # --force overrides and deletes cleanly.
        ok_code, ok_out, ok_err = self._run_cli("delete", "CBRD-202", "--force", "--json")
        self.assertEqual(ok_code, 0, ok_err)
        self.assertTrue(json.loads(ok_out)["data"]["deleted"])
        self.assertFalse(wt.exists())

    def test_delete_refuses_dirty_superproject_without_force_and_allows_force(self) -> None:
        code, _, err = self._run_cli("create", "CBRD-203", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-203"
        (wt / "dirty.txt").write_text("uncommitted\n")

        fail_code, _, fail_err = self._run_cli("delete", "CBRD-203", "--json")
        self.assertEqual(fail_code, 1)
        self.assertEqual(json.loads(fail_err)["error"]["code"], "DIRTY_WORKTREE")
        self.assertTrue(wt.exists())

        ok_code, ok_out, ok_err = self._run_cli("delete", "CBRD-203", "--force", "--json")
        self.assertEqual(ok_code, 0, ok_err)
        self.assertTrue(json.loads(ok_out)["data"]["deleted"])
        self.assertFalse(wt.exists())

    def test_delete_refuses_protected_main_control_locked_and_active_cwd(self) -> None:
        prot_code, _, prot_err = self._run_cli("delete", "develop", "--force", "--json")
        self.assertEqual(prot_code, 1)
        self.assertEqual(json.loads(prot_err)["error"]["code"], "PROTECTED_WORKTREE")
        self.assertTrue(self.control.is_dir())

        miss_code, _, miss_err = self._run_cli("delete", "CBRD-99999", "--json")
        self.assertEqual(miss_code, 1)
        self.assertEqual(json.loads(miss_err)["error"]["code"], "WORKTREE_NOT_FOUND")

        code, _, err = self._run_cli("create", "CBRD-204", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-204"
        subdir = wt / "src"
        subdir.mkdir()
        with mock.patch("cubrid_worktree.git_ops.Path.cwd", return_value=subdir):
            cwd_code, _, cwd_err = self._run_cli("delete", "CBRD-204", "--force", "--json")
        self.assertEqual(cwd_code, 1)
        self.assertEqual(json.loads(cwd_err)["error"]["code"], "ACTIVE_CWD")
        subdir.rmdir()

        self._git(self.control, "worktree", "lock", "--reason", "in use", wt)
        lock_code, _, lock_err = self._run_cli("delete", "CBRD-204", "--force", "--json")
        self.assertEqual(lock_code, 1)
        lock_payload = json.loads(lock_err)["error"]
        self.assertEqual(lock_payload["code"], "WORKTREE_LOCKED")
        self.assertIn("in use", lock_payload["message"])

        self._git(self.control, "worktree", "unlock", wt)
        ok_code, ok_out, ok_err = self._run_cli("delete", "CBRD-204", "--json")
        self.assertEqual(ok_code, 0, ok_err)
        self.assertTrue(json.loads(ok_out)["data"]["deleted"])
        self.assertFalse(wt.exists())

    def test_delete_dry_run_performs_no_mutations_even_with_submodules(self) -> None:
        self._create_submodule_remote("cubrid-cci", add_to_seed_at="cubrid-cci")

        code, _, err = self._run_cli("create", "CBRD-205", "--no-prepare", "--json")
        self.assertEqual(code, 0, err)
        wt = self.parent / "CBRD-205"
        self._git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "cubrid-cci")

        dry_code, dry_out, dry_err = self._run_cli("delete", "CBRD-205", "--dry-run", "--delete-branch", "--json")
        self.assertEqual(dry_code, 0, dry_err)
        data = json.loads(dry_out)["data"]
        self.assertTrue(data["dry_run"])
        self.assertFalse(data["deleted"])
        self.assertEqual(data["submodules"]["populated"], ["cubrid-cci"])
        self.assertTrue(data["submodules"]["modules_gitdir_present"])
        self.assertFalse(data["submodules"]["modules_gitdir_removed"])
        self.assertEqual(data["branch_deletion"]["status"], "dry_run")
        self.assertTrue(wt.is_dir())
        self.assertTrue((wt / "cubrid-cci" / ".git").exists())
        self.assertEqual(
            self._git(self.control, "show-ref", "--verify", "--quiet", "refs/heads/CBRD-205", check=False).returncode,
            0,
        )

    def test_delete_human_and_plain_outputs(self) -> None:
        self._create_submodule_remote("cubrid-cci", add_to_seed_at="cubrid-cci")
        code1, _, err1 = self._run_cli("create", "CBRD-206", "--no-prepare", "--json")
        self.assertEqual(code1, 0, err1)
        wt1 = self.parent / "CBRD-206"
        self._git(wt1, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "cubrid-cci")

        h_code, h_out, h_err = self._run_cli("delete", "CBRD-206")
        self.assertEqual(h_code, 0, h_err)
        self.assertIn(f"Deleted worktree: {wt1}", h_out)
        self.assertIn("Branch: CBRD-206 (kept)", h_out)
        self.assertIn("Role: implement", h_out)
        self.assertIn("Submodules: cleaned (cubrid-cci); modules gitdir removed", h_out)

        code2, _, err2 = self._run_cli("create", "CBRD-207", "--no-prepare", "--json")
        self.assertEqual(code2, 0, err2)
        wt2 = self.parent / "CBRD-207"
        p_code, p_out, p_err = self._run_cli("delete", "CBRD-207", "--plain")
        self.assertEqual(p_code, 0, p_err)
        self.assertEqual(p_out.strip(), f"CBRD-207\tCBRD-207\timplement\t{wt2}")


if __name__ == "__main__":
    unittest.main()
