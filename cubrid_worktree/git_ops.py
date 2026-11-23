"""Git worktree, branch, and metadata operations."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import CliError
from .naming import extract_single_ticket

LEGACY_UNTICKETED_REVIEW_RE = re.compile(r"^review-noticket-pr-([1-9][0-9]*)$", re.IGNORECASE)
PR_NUMBER_URL_RE = re.compile(r"/pull/([1-9][0-9]*)/?$")


@dataclass(frozen=True)
class WorktreeRecord:
    path: Path
    head: str
    branch: str | None
    detached: bool
    pr_url: str | None
    role: str | None
    ticket: str | None

    @property
    def name(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = os.fspath(self.path)
        payload["name"] = self.name
        return payload


def run_git(
    args: list[str],
    *,
    cwd: Path,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError("MISSING_COMMAND", "Required command is not installed: git.", fix="Install git and ensure it is on PATH.") from exc

    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(
            "GIT_FAILED",
            f"Git command failed ({' '.join(command)}): {detail or f'exit {result.returncode}'}",
            fix="Inspect the repository and branch state in the control checkout.",
        )
    return result


def ensure_control_worktree(control_dir: Path, parent_dir: Path) -> Path:
    if not control_dir.is_dir():
        raise CliError(
            "MISSING_CONTROL_DIR",
            f"Control worktree does not exist: {control_dir}",
            fix="Pass --control-dir or set CUBRID_WORKTREE_CONTROL_DIR to your develop checkout.",
        )
    if not parent_dir.is_dir():
        raise CliError(
            "MISSING_PARENT_DIR",
            f"Worktree parent directory does not exist: {parent_dir}",
            fix="Create the parent directory or pass --parent-dir.",
        )
    root = Path(run_git(["rev-parse", "--show-toplevel"], cwd=control_dir).stdout.strip()).resolve()
    if root != control_dir.resolve():
        raise CliError(
            "INVALID_CONTROL_DIR",
            f"Control path is not a Git worktree root: {control_dir} (root is {root})",
            fix="Point --control-dir at the root of the control checkout.",
        )
    return root


def _branch_metadata(control_dir: Path) -> dict[str, dict[str, str]]:
    result = run_git(
        ["config", "--local", "--get-regexp", r"^branch\..*\.(pr-url|cubrid-role)$"],
        cwd=control_dir,
        allowed=(0, 1),
    )
    metadata: dict[str, dict[str, str]] = {}
    if result.returncode != 0 or not result.stdout:
        return metadata
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        if not key.startswith("branch."):
            continue
        remainder = key.removeprefix("branch.")
        if "." not in remainder:
            continue
        branch_name, attr = remainder.rsplit(".", 1)
        metadata.setdefault(branch_name, {})[attr] = value.strip()
    return metadata


def list_worktrees(control_dir: Path) -> list[WorktreeRecord]:
    result = run_git(["worktree", "list", "--porcelain", "-z"], cwd=control_dir)
    meta = _branch_metadata(control_dir)
    records: list[WorktreeRecord] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        if "path" not in current:
            return
        path = Path(current["path"]).resolve()
        branch = current.get("branch")
        branch_meta = meta.get(branch, {}) if branch else {}
        ticket = extract_single_ticket(path.name) or (extract_single_ticket(branch) if branch else None)
        records.append(
            WorktreeRecord(
                path=path,
                head=current.get("head", ""),
                branch=branch,
                detached=bool(current.get("detached", False)),
                pr_url=branch_meta.get("pr-url"),
                role=branch_meta.get("cubrid-role"),
                ticket=ticket,
            )
        )

    for token in result.stdout.split("\0"):
        if token == "":
            flush()
            current = {}
            continue
        if token.startswith("worktree "):
            current["path"] = token.removeprefix("worktree ")
        elif token.startswith("HEAD "):
            current["head"] = token.removeprefix("HEAD ")
        elif token.startswith("branch refs/heads/"):
            current["branch"] = token.removeprefix("branch refs/heads/")
        elif token == "detached":
            current["detached"] = True

    flush()
    return records


def local_branch_exists(control_dir: Path, branch: str) -> bool:
    result = run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=control_dir,
        allowed=(0, 1),
    )
    return result.returncode == 0


def validate_branch_name(control_dir: Path, branch: str) -> None:
    result = run_git(
        ["check-ref-format", "--branch", branch],
        cwd=control_dir,
        allowed=(0, 1, 128),
    )
    if result.returncode != 0:
        raise CliError(
            "INVALID_BRANCH",
            f"Invalid Git branch name: {branch}",
            fix="Choose a valid branch name without spaces or special Git ref syntax.",
            exit_code=2,
        )


def is_target_occupied(
    control_dir: Path,
    parent_dir: Path,
    name: str,
    worktrees: list[WorktreeRecord] | None = None,
) -> bool:
    target_path = parent_dir / name
    if os.path.lexists(target_path):
        return True
    resolved_target = target_path.resolve()
    records = list_worktrees(control_dir) if worktrees is None else worktrees
    if any(record.path == resolved_target for record in records):
        return True
    return local_branch_exists(control_dir, name)


def exact_conflict_error(
    control_dir: Path,
    target_path: Path,
    branch: str,
    worktrees: list[WorktreeRecord] | None = None,
) -> str | None:
    if os.path.lexists(target_path):
        return f"worktree path already exists: {target_path}"
    resolved_target = target_path.resolve()
    records = list_worktrees(control_dir) if worktrees is None else worktrees
    if any(record.path == resolved_target for record in records):
        return f"worktree path is already registered: {target_path}"
    if local_branch_exists(control_dir, branch):
        return f"local branch already exists: {branch}"
    return None


def find_ticket_siblings(
    worktrees: list[WorktreeRecord],
    parent_dir: Path,
    ticket: str | None,
    *,
    exclude_path: Path | None = None,
) -> list[dict[str, str]]:
    if not ticket:
        return []
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticket)}(?![A-Za-z0-9])", re.IGNORECASE)
    excluded = exclude_path.resolve() if exclude_path is not None else None
    seen_paths: set[Path] = set()
    siblings: list[dict[str, str]] = []

    for record in worktrees:
        if excluded is not None and record.path == excluded:
            continue
        if pattern.search(record.path.name) or (record.branch and pattern.search(record.branch)):
            seen_paths.add(record.path)
            siblings.append(
                {
                    "source": "registered worktree",
                    "name": record.path.name,
                    "path": os.fspath(record.path),
                    "branch": record.branch or "(detached)",
                    "role": record.role or "",
                }
            )

    if parent_dir.is_dir():
        for child in sorted(parent_dir.iterdir(), key=lambda entry: entry.name.casefold()):
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if excluded is not None and resolved == excluded:
                continue
            if resolved in seen_paths:
                continue
            if pattern.search(child.name):
                siblings.append(
                    {
                        "source": "sibling directory",
                        "name": child.name,
                        "path": os.fspath(child),
                        "branch": "",
                        "role": "",
                    }
                )

    return siblings


def find_existing_pr_worktree(
    worktrees: list[WorktreeRecord],
    pr_number: int,
    pr_url: str,
    ticket: str | None,
) -> WorktreeRecord | None:
    canonical = pr_url.rstrip("/").casefold()
    for record in worktrees:
        if record.pr_url and record.pr_url.rstrip("/").casefold() == canonical:
            return record

    expected_names = {f"PR-{pr_number}".casefold(), f"review-noticket-pr-{pr_number}".casefold()}
    if ticket:
        expected_names.add(f"{ticket}-pr-{pr_number}".casefold())
        expected_names.add(f"review-{ticket}-pr-{pr_number}".casefold())

    for record in worktrees:
        if record.path.name.casefold() in expected_names:
            return record
        if record.branch and record.branch.casefold() in expected_names:
            return record
    return None


def configured_remotes(control_dir: Path) -> set[str]:
    result = run_git(["remote"], cwd=control_dir)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def fetch_base_if_remote(control_dir: Path, base_ref: str) -> dict[str, Any]:
    if "/" not in base_ref:
        return {"status": "skipped", "reason": "local ref", "ref": base_ref}
    remote, branch = base_ref.split("/", 1)
    remotes = configured_remotes(control_dir)
    if remote not in remotes:
        return {"status": "skipped", "reason": f"'{remote}' is not a configured remote", "ref": base_ref}
    run_git(["fetch", "--", remote, branch], cwd=control_dir)
    return {"status": "fetched", "remote": remote, "branch": branch, "ref": base_ref}


def set_branch_role(control_dir: Path, branch: str, role: str) -> None:
    run_git(["config", "--local", "--replace-all", f"branch.{branch}.cubrid-role", role], cwd=control_dir)


def set_branch_pr_url(control_dir: Path, branch: str, pr_url: str) -> None:
    run_git(["config", "--local", "--replace-all", f"branch.{branch}.pr-url", pr_url], cwd=control_dir)


def add_worktree(control_dir: Path, target_path: Path, branch: str, base_ref: str) -> None:
    run_git(
        ["worktree", "add", "-b", branch, os.fspath(target_path), base_ref],
        cwd=control_dir,
    )
    set_branch_role(control_dir, branch, "implement")
