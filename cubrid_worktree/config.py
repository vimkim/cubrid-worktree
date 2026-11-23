"""Configuration resolution following flags > environment > defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    control_dir: Path
    parent_dir: Path
    repository: str
    prepare_justfile: Path
    prepare_cmd: str | None
    review_candidates_cmd: str | None


def _env_nonempty(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def resolve_config(
    *,
    control_dir: str | Path | None = None,
    parent_dir: str | Path | None = None,
    repository: str | None = None,
    prepare_justfile: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    active_env = os.environ if env is None else env
    home = Path(active_env.get("HOME", "~")).expanduser() if active_env.get("HOME") else Path.home()

    raw_control = (
        str(control_dir)
        if control_dir is not None
        else _env_nonempty(
            active_env, "CUBRID_WORKTREE_CONTROL_DIR", "CUBRID_REVIEW_CONTROL_DIR"
        )
    )
    resolved_control = (
        Path(raw_control).expanduser()
        if raw_control is not None
        else home / "gh" / "cb" / "develop"
    )

    raw_parent = (
        str(parent_dir)
        if parent_dir is not None
        else _env_nonempty(
            active_env, "CUBRID_WORKTREE_PARENT", "CUBRID_REVIEW_WORKTREE_PARENT"
        )
    )
    resolved_parent = (
        Path(raw_parent).expanduser()
        if raw_parent is not None
        else resolved_control.parent
    )

    resolved_repo = (
        repository.strip()
        if repository is not None and repository.strip()
        else _env_nonempty(active_env, "CUBRID_WORKTREE_REPO") or "CUBRID/cubrid"
    )

    raw_justfile = (
        str(prepare_justfile)
        if prepare_justfile is not None
        else _env_nonempty(
            active_env, "CUBRID_WORKTREE_PREPARE_JUSTFILE", "CUBRID_REVIEW_JUSTFILE"
        )
    )
    if raw_justfile is not None:
        resolved_justfile = Path(raw_justfile).expanduser()
    else:
        my_cubrid_raw = _env_nonempty(active_env, "MY_CUBRID")
        my_cubrid_dir = Path(my_cubrid_raw).expanduser() if my_cubrid_raw else home / "my-cubrid"
        resolved_justfile = my_cubrid_dir / "cubrid-justfiles" / "justfile"

    prepare_cmd = _env_nonempty(active_env, "CUBRID_WORKTREE_PREPARE_CMD")
    review_candidates_cmd = _env_nonempty(active_env, "CUBRID_WORKTREE_REVIEW_CANDIDATES_CMD")

    return Config(
        control_dir=resolved_control,
        parent_dir=resolved_parent,
        repository=resolved_repo,
        prepare_justfile=resolved_justfile,
        prepare_cmd=prepare_cmd,
        review_candidates_cmd=review_candidates_cmd,
    )
