"""Herdr Space-per-ticket and Tab-per-derived-worktree placement."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import CliError


def _run_json_cmd(cmd: list[str], *, allowed: tuple[int, ...] = (0,)) -> tuple[int, dict[str, Any]]:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError(
            "MISSING_COMMAND",
            f"Required Herdr command is not installed: {cmd[0]}",
            fix="Ensure herdr is on PATH or pass --no-herdr.",
        ) from exc

    payload: dict[str, Any] = {}
    if result.stdout.strip():
        try:
            decoded = json.loads(result.stdout)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}

    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(
            "HERDR_FAILED",
            f"Herdr command failed ({' '.join(cmd)}): {detail or f'exit {result.returncode}'}",
            fix="Check `herdr status` or open the worktree manually.",
        )
    return result.returncode, payload


def _find_workspace(label: str) -> dict[str, Any] | None:
    _, data = _run_json_cmd(["herdr", "workspace", "list"])
    workspaces = data.get("result", {}).get("workspaces", [])
    if not isinstance(workspaces, list):
        return None
    target = label.casefold()
    for ws in workspaces:
        if isinstance(ws, dict) and str(ws.get("label", "")).casefold() == target:
            return ws
    return None


def _find_matching_pane(workspace_id: str, target_path: Path) -> dict[str, Any] | None:
    _, data = _run_json_cmd(["herdr", "pane", "list", "--workspace", workspace_id])
    panes = data.get("result", {}).get("panes", [])
    if not isinstance(panes, list):
        return None
    resolved = target_path.resolve()
    for pane in panes:
        if not isinstance(pane, dict):
            continue
        for key in ("cwd", "foreground_cwd"):
            raw = pane.get(key)
            if not raw:
                continue
            try:
                if Path(str(raw)).resolve() == resolved:
                    return pane
            except OSError:
                continue
    return None


def _active_tab_id(workspace_id: str) -> str | None:
    _, data = _run_json_cmd(["herdr", "workspace", "get", workspace_id])
    ws = data.get("result", {}).get("workspace", {})
    if isinstance(ws, dict):
        tab_id = ws.get("active_tab_id")
        return str(tab_id) if isinstance(tab_id, str) and tab_id else None
    return None


def _create_space(
    target_path: Path,
    space_label: str,
    *,
    focus: bool,
) -> tuple[str, str | None, str | None]:
    next_to_current = shutil.which("herdr-next-to-current")
    if next_to_current:
        cmd = [
            "herdr-next-to-current",
            "workspace",
            "--cwd",
            os.fspath(target_path),
            "--label",
            space_label,
            "--json",
        ]
        if not focus:
            cmd.append("--no-focus")
        rc, data = _run_json_cmd(cmd, allowed=(0, 2, 3, 4))
        new_id = data.get("new_id")
        if rc == 0 and isinstance(new_id, str) and new_id:
            return new_id, _active_tab_id(new_id), None
        if isinstance(new_id, str) and new_id:
            # Workspace was already created before a partial placement step; keep it rather than duplicating.
            return new_id, _active_tab_id(new_id), None

    cmd = [
        "herdr",
        "workspace",
        "create",
        "--cwd",
        os.fspath(target_path),
        "--label",
        space_label,
        "--focus" if focus else "--no-focus",
    ]
    _, data = _run_json_cmd(cmd)
    result = data.get("result", {})
    ws = result.get("workspace", {}) if isinstance(result, dict) else {}
    tab = result.get("tab", {}) if isinstance(result, dict) else {}
    pane = result.get("root_pane", {}) if isinstance(result, dict) else {}
    workspace_id = ws.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise CliError(
            "HERDR_FAILED",
            "Herdr workspace create succeeded without returning workspace_id.",
            fix="Check `herdr workspace list`.",
        )
    tab_id = tab.get("tab_id") if isinstance(tab, dict) else None
    pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
    return workspace_id, tab_id if isinstance(tab_id, str) else None, pane_id if isinstance(pane_id, str) else None


def open_in_herdr(
    target_path: Path,
    space_label: str,
    tab_label: str | None = None,
    *,
    focus: bool = True,
    new_tab: bool = True,
    force: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    active_env = os.environ if env is None else env
    if active_env.get("HERDR_ENV") != "1":
        if force:
            raise CliError(
                "HERDR_UNAVAILABLE",
                "HERDR_ENV is not 1; cannot control Herdr outside a Herdr-managed pane.",
                fix="Run this command from a Herdr pane or pass --no-herdr.",
            )
        return {"status": "skipped", "reason": "not inside Herdr (HERDR_ENV!=1)"}

    if dry_run:
        return {
            "status": "dry_run",
            "space_label": space_label,
            "tab_label": tab_label,
            "cwd": os.fspath(target_path),
            "focus": focus,
            "new_tab": new_tab,
        }

    existing_ws = _find_workspace(space_label)
    if existing_ws is not None:
        workspace_id = str(existing_ws["workspace_id"])
        actual_space_label = str(existing_ws.get("label", space_label))
        if not new_tab:
            matched_pane = _find_matching_pane(workspace_id, target_path)
            if matched_pane is not None:
                tab_id = str(matched_pane["tab_id"])
                pane_id = str(matched_pane["pane_id"])
                if focus:
                    _run_json_cmd(["herdr", "tab", "focus", tab_id])
                    _run_json_cmd(["herdr", "workspace", "focus", workspace_id])
                return {
                    "status": "focused_existing",
                    "workspace_id": workspace_id,
                    "tab_id": tab_id,
                    "pane_id": pane_id,
                    "space_label": actual_space_label,
                    "tab_label": tab_label,
                }

        effective_tab_label = tab_label or target_path.name
        cmd = [
            "herdr",
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            os.fspath(target_path),
            "--label",
            effective_tab_label,
            "--focus" if focus else "--no-focus",
        ]
        _, created = _run_json_cmd(cmd)
        result = created.get("result", {})
        tab = result.get("tab", {}) if isinstance(result, dict) else {}
        pane = result.get("root_pane", {}) if isinstance(result, dict) else {}
        tab_id = tab.get("tab_id") if isinstance(tab, dict) else None
        pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
        if focus:
            _run_json_cmd(["herdr", "workspace", "focus", workspace_id])
        return {
            "status": "tab_created",
            "workspace_id": workspace_id,
            "tab_id": tab_id,
            "pane_id": pane_id,
            "space_label": actual_space_label,
            "tab_label": effective_tab_label,
        }

    workspace_id, tab_id, pane_id = _create_space(target_path, space_label, focus=focus)
    if tab_label and tab_id:
        _run_json_cmd(["herdr", "tab", "rename", tab_id, tab_label])

    return {
        "status": "workspace_created",
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "space_label": space_label,
        "tab_label": tab_label,
    }
