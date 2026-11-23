"""Tests for Herdr 1-Space-per-ticket and Tab-per-derived-worktree placement."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cubrid_worktree.errors import CliError
from cubrid_worktree.herdr import open_in_herdr


class HerdrPlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.primary_path = self.root / "CBRD-28099"
        self.derived_path = self.root / "CBRD-28099-bench"
        self.primary_path.mkdir()
        self.derived_path.mkdir()
        self.commands: list[list[str]] = []

    def test_outside_herdr_skips_by_default_and_errors_when_forced(self) -> None:
        res = open_in_herdr(self.primary_path, "CBRD-28099", env={"HERDR_ENV": "0"})
        self.assertEqual(res["status"], "skipped")

        with self.assertRaises(CliError) as ctx:
            open_in_herdr(self.primary_path, "CBRD-28099", force=True, env={"HERDR_ENV": "0"})
        self.assertEqual(ctx.exception.code, "HERDR_UNAVAILABLE")

    def test_creates_new_space_when_ticket_space_absent_and_renames_initial_tab_for_summary(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(cmd))
            if cmd[:3] == ["herdr", "workspace", "list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"result":{"workspaces":[{"workspace_id":"w1","label":"develop"}]}}', ""
                )
            if cmd[:2] == ["herdr-next-to-current", "workspace"]:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"status":"complete","source_id":"w1","new_id":"w2","stage":"focused","code":0}', ""
                )
            if cmd[:3] == ["herdr", "workspace", "get"]:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"result":{"workspace":{"workspace_id":"w2","active_tab_id":"w2:t1"}}}', ""
                )
            if cmd[:3] == ["herdr", "tab", "rename"]:
                return subprocess.CompletedProcess(cmd, 0, "{}", "")
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch("shutil.which", return_value="/usr/bin/herdr-next-to-current"),
        ):
            res = open_in_herdr(
                self.derived_path,
                "CBRD-28099",
                tab_label="bench",
                env={"HERDR_ENV": "1"},
            )

        self.assertEqual(res["status"], "workspace_created")
        self.assertEqual(res["workspace_id"], "w2")
        self.assertEqual(res["tab_id"], "w2:t1")
        self.assertEqual(self.commands[-1], ["herdr", "tab", "rename", "w2:t1", "bench"])

    def test_creates_tab_inside_existing_ticket_space_and_focuses(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(cmd))
            if cmd[:3] == ["herdr", "workspace", "list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"result":{"workspaces":[{"workspace_id":"w9","label":"CBRD-28099"}]}}', ""
                )
            if cmd[:3] == ["herdr", "tab", "create"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    '{"result":{"tab":{"tab_id":"w9:t2"},"root_pane":{"pane_id":"w9:p2"}}}',
                    "",
                )
            if cmd[:3] == ["herdr", "workspace", "focus"]:
                return subprocess.CompletedProcess(cmd, 0, "{}", "")
            raise AssertionError(f"Unexpected command: {cmd}")

        with mock.patch("subprocess.run", side_effect=fake_run):
            res = open_in_herdr(
                self.derived_path,
                "CBRD-28099",
                tab_label="bench",
                focus=True,
                new_tab=True,
                env={"HERDR_ENV": "1"},
            )

        self.assertEqual(res["status"], "tab_created")
        self.assertEqual(res["workspace_id"], "w9")
        self.assertEqual(res["tab_id"], "w9:t2")
        self.assertEqual(res["pane_id"], "w9:p2")
        self.assertIn(
            [
                "herdr",
                "tab",
                "create",
                "--workspace",
                "w9",
                "--cwd",
                os.fspath(self.derived_path),
                "--label",
                "bench",
                "--focus",
            ],
            self.commands,
        )
        self.assertEqual(self.commands[-1], ["herdr", "workspace", "focus", "w9"])

    def test_falls_back_to_herdr_workspace_create_and_focuses_existing_pane_on_open(self) -> None:
        def fake_run_fallback(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(cmd))
            if cmd[:3] == ["herdr", "workspace", "list"]:
                return subprocess.CompletedProcess(cmd, 0, '{"result":{"workspaces":[]}}', "")
            if cmd[:3] == ["herdr", "workspace", "create"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    '{"result":{"workspace":{"workspace_id":"w15"},"tab":{"tab_id":"w15:t1"},"root_pane":{"pane_id":"w15:p1"}}}',
                    "",
                )
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            mock.patch("subprocess.run", side_effect=fake_run_fallback),
            mock.patch("shutil.which", return_value=None),
        ):
            res = open_in_herdr(
                self.primary_path,
                "CBRD-28099",
                focus=False,
                env={"HERDR_ENV": "1"},
            )
        self.assertEqual(res["status"], "workspace_created")
        self.assertEqual(res["workspace_id"], "w15")
        self.assertEqual(res["pane_id"], "w15:p1")

        # Existing pane at matching cwd focuses without creating a duplicate tab when new_tab=False.
        self.commands.clear()

        def fake_run_existing(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(list(cmd))
            if cmd[:3] == ["herdr", "workspace", "list"]:
                return subprocess.CompletedProcess(
                    cmd, 0, '{"result":{"workspaces":[{"workspace_id":"w15","label":"CBRD-28099"}]}}', ""
                )
            if cmd[:3] == ["herdr", "pane", "list"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    f'{{"result":{{"panes":[{{"pane_id":"w15:p1","tab_id":"w15:t1","cwd":{json_str(self.primary_path)}}}]}}}}',
                    "",
                )
            if cmd[:3] in (["herdr", "tab", "focus"], ["herdr", "workspace", "focus"]):
                return subprocess.CompletedProcess(cmd, 0, "{}", "")
            raise AssertionError(f"Unexpected command: {cmd}")

        with mock.patch("subprocess.run", side_effect=fake_run_existing):
            res_open = open_in_herdr(
                self.primary_path,
                "CBRD-28099",
                new_tab=False,
                focus=True,
                env={"HERDR_ENV": "1"},
            )
        self.assertEqual(res_open["status"], "focused_existing")
        self.assertEqual(res_open["tab_id"], "w15:t1")


def json_str(path: Path) -> str:
    import json

    return json.dumps(os.fspath(path))


if __name__ == "__main__":
    unittest.main()
