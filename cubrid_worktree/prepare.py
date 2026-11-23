"""Worktree build-environment preparation helper."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .errors import CliError


def prepare_command(config: Config, target_path: Path) -> list[str] | None:
    if config.prepare_cmd:
        tokens = shlex.split(config.prepare_cmd)
        return [token.replace("{path}", os.fspath(target_path)) for token in tokens]
    if config.prepare_justfile.is_file():
        return [
            "just",
            "--justfile",
            os.fspath(config.prepare_justfile),
            "--working-directory",
            os.fspath(target_path),
            "prepare-build",
        ]
    return None


def is_worktree_prepared(target_path: Path) -> bool:
    required = ("justfile", ".envrc", "CMakeUserPresets.json", ".env")
    return all((target_path / name).exists() for name in required)


def run_prepare(config: Config, target_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    cmd = prepare_command(config, target_path)
    if cmd is None:
        return {
            "status": "skipped",
            "reason": "prepare justfile not found",
            "justfile": os.fspath(config.prepare_justfile),
        }

    command_str = shlex.join(cmd)
    if dry_run:
        return {"status": "dry_run", "command": cmd}

    try:
        result = subprocess.run(
            cmd,
            cwd=target_path,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError(
            "MISSING_COMMAND",
            f"Preparation command not found: {cmd[0]}",
            fix=f"Install {cmd[0]} or pass --no-prepare. Retry preparation with: {command_str}",
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise CliError(
            "PREPARE_FAILED",
            f"Worktree preparation failed with exit status {result.returncode}{suffix}",
            fix=f"The worktree remains at {target_path}. Retry preparation with: {command_str}",
        )

    return {"status": "prepared", "command": cmd}
